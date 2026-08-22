"""align yawn_core schema with models

合并替代被作废的 a0445ad1df97（webui 全量 diff，错挂在 RPG 主链上，
跨分支依赖 d4a7f9c2e610 创建的 webadminaudit 表，且破坏性重建
agentmediacache）与 885838d85f60（agentmemory 唯一键修复）。

本迁移挂在 yawn_core 分支的 d4a7f9c2e610 之后，净效果：
- 各 Agent 相关表的业务列移除 server_default（模型改用 Python 端
  default=；compare_server_default 开启时这些差异必须落库）；
- agentmemory 唯一约束 uq_agent_memory_key 扩为含 memory_type 的 5 列；
- groupagentconfig 移除废弃的 expires_at 列；
- 补齐模型声明而旧迁移遗漏的索引；
- webadminaudit 的 request_id 索引改为唯一。

对 agentmediacache 只移除 4 个 server_default：group_id 外键与各索引
在 b7e1c3a9f204 建表时已存在（无命名外键在 SQLite 下即模型目标态），
无需重建表。created_at/last_access_at 的 CURRENT_TIMESTAMP 默认值模型
仍然声明，保持不动。

downgrade 局限：把 uq_agent_memory_key 收窄回 4 列时，若存量数据存在
仅 memory_type 不同的行会触发 IntegrityError。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite


revision: str = "c3d8e5f17a92"
down_revision: str | Sequence[str] | None = "d4a7f9c2e610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table("yawn_core_agentaudit", schema=None) as batch_op:
        batch_op.alter_column(
            "result",
            existing_type=sa.VARCHAR(length=24),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.create_index(
            "ix_yawn_core_agentaudit_created_at", ["created_at"], unique=False
        )
        batch_op.create_index(
            "ix_yawn_core_agentaudit_group_id", ["group_id"], unique=False
        )

    with op.batch_alter_table("yawn_core_agentmediacache", schema=None) as batch_op:
        batch_op.alter_column(
            "media_type",
            existing_type=sa.VARCHAR(length=24),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "model_name",
            existing_type=sa.VARCHAR(length=128),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "status",
            existing_type=sa.VARCHAR(length=24),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "size_bytes",
            existing_type=sa.INTEGER(),
            server_default=None,
            existing_nullable=False,
        )

    with op.batch_alter_table("yawn_core_agentmemory", schema=None) as batch_op:
        batch_op.alter_column(
            "scope",
            existing_type=sa.VARCHAR(length=16),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "memory_type",
            existing_type=sa.VARCHAR(length=24),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "salience",
            existing_type=sa.FLOAT(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "confidence",
            existing_type=sa.FLOAT(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "visibility",
            existing_type=sa.VARCHAR(length=24),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.create_index(
            "ix_yawn_core_agentmemory_group_id", ["group_id"], unique=False
        )
        batch_op.create_index(
            "ix_yawn_core_agentmemory_memory_type", ["memory_type"], unique=False
        )
        batch_op.create_index(
            "ix_yawn_core_agentmemory_scope", ["scope"], unique=False
        )
        batch_op.create_index(
            "ix_yawn_core_agentmemory_subject_user_id",
            ["subject_user_id"],
            unique=False,
        )
        # 加列只会放宽约束，存量数据不可能违反新唯一键，升级安全。
        batch_op.drop_constraint("uq_agent_memory_key", type_="unique")
        batch_op.create_unique_constraint(
            "uq_agent_memory_key",
            ["scope", "group_id", "subject_user_id", "memory_type", "memory_key"],
        )

    with op.batch_alter_table("yawn_core_agentprivacy", schema=None) as batch_op:
        batch_op.alter_column(
            "opted_out",
            existing_type=sa.BOOLEAN(),
            server_default=None,
            existing_nullable=False,
        )

    with op.batch_alter_table("yawn_core_agentrelation", schema=None) as batch_op:
        batch_op.alter_column(
            "confidence",
            existing_type=sa.FLOAT(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "evidence_count",
            existing_type=sa.INTEGER(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.create_index(
            "ix_yawn_core_agentrelation_group_id", ["group_id"], unique=False
        )
        batch_op.create_index(
            "ix_yawn_core_agentrelation_object_user_id",
            ["object_user_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_yawn_core_agentrelation_subject_user_id",
            ["subject_user_id"],
            unique=False,
        )

    with op.batch_alter_table("yawn_core_groupagentconfig", schema=None) as batch_op:
        batch_op.alter_column(
            "enabled",
            existing_type=sa.BOOLEAN(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "persona",
            existing_type=sa.TEXT(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "persona_override",
            existing_type=sqlite.JSON(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "persona_enabled",
            existing_type=sa.BOOLEAN(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "persona_version",
            existing_type=sa.INTEGER(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "trigger_mode",
            existing_type=sa.VARCHAR(length=24),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "proactive_probability",
            existing_type=sa.FLOAT(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "idle_threshold_minutes",
            existing_type=sa.INTEGER(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "cooldown_minutes",
            existing_type=sa.INTEGER(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "daily_limit",
            existing_type=sa.INTEGER(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "raw_retention_days",
            existing_type=sa.INTEGER(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "cross_group_visibility",
            existing_type=sa.VARCHAR(length=24),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "media_cache_enabled",
            existing_type=sa.BOOLEAN(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "proactive_count",
            existing_type=sa.INTEGER(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "emotion_state",
            existing_type=sqlite.JSON(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "recent_response_fingerprints",
            existing_type=sqlite.JSON(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "context_epoch",
            existing_type=sa.INTEGER(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "admin_tool_count",
            existing_type=sa.INTEGER(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "admin_tool_daily_limit",
            existing_type=sa.INTEGER(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "tool_allowlist",
            existing_type=sqlite.JSON(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.drop_column("expires_at")

    with op.batch_alter_table("yawn_core_groupagentmessage", schema=None) as batch_op:
        batch_op.alter_column(
            "role",
            existing_type=sa.VARCHAR(length=16),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "normalized_text",
            existing_type=sa.TEXT(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.create_index(
            "ix_yawn_core_groupagentmessage_bot_id", ["bot_id"], unique=False
        )
        batch_op.create_index(
            "ix_yawn_core_groupagentmessage_expires_at", ["expires_at"], unique=False
        )
        batch_op.create_index(
            "ix_yawn_core_groupagentmessage_group_id", ["group_id"], unique=False
        )
        batch_op.create_index(
            "ix_yawn_core_groupagentmessage_message_id", ["message_id"], unique=False
        )
        batch_op.create_index(
            "ix_yawn_core_groupagentmessage_received_at",
            ["received_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_yawn_core_groupagentmessage_user_id", ["user_id"], unique=False
        )

    with op.batch_alter_table("yawn_core_usergroup", schema=None) as batch_op:
        batch_op.alter_column(
            "role",
            existing_type=sa.VARCHAR(length=16),
            server_default=None,
            existing_nullable=False,
        )

    # d4a7f9c2e610 已在本分支创建该表，此处仅对齐模型：去默认值 +
    # request_id 索引升级为唯一。
    with op.batch_alter_table("yawn_core_webadminaudit", schema=None) as batch_op:
        batch_op.alter_column(
            "result",
            existing_type=sa.VARCHAR(length=24),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "detail",
            existing_type=sqlite.JSON(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.drop_index("ix_yawn_core_webadminaudit_request_id")
        batch_op.create_index(
            "ix_yawn_core_webadminaudit_request_id", ["request_id"], unique=True
        )


def downgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table("yawn_core_webadminaudit", schema=None) as batch_op:
        batch_op.drop_index("ix_yawn_core_webadminaudit_request_id")
        batch_op.create_index(
            "ix_yawn_core_webadminaudit_request_id", ["request_id"], unique=False
        )
        batch_op.alter_column(
            "detail",
            existing_type=sqlite.JSON(),
            server_default=sa.text("'{}'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "result",
            existing_type=sa.VARCHAR(length=24),
            server_default=sa.text("'success'"),
            existing_nullable=False,
        )

    with op.batch_alter_table("yawn_core_usergroup", schema=None) as batch_op:
        batch_op.alter_column(
            "role",
            existing_type=sa.VARCHAR(length=16),
            server_default=sa.text("'member'"),
            existing_nullable=False,
        )

    with op.batch_alter_table("yawn_core_groupagentmessage", schema=None) as batch_op:
        batch_op.drop_index("ix_yawn_core_groupagentmessage_user_id")
        batch_op.drop_index("ix_yawn_core_groupagentmessage_received_at")
        batch_op.drop_index("ix_yawn_core_groupagentmessage_message_id")
        batch_op.drop_index("ix_yawn_core_groupagentmessage_group_id")
        batch_op.drop_index("ix_yawn_core_groupagentmessage_expires_at")
        batch_op.drop_index("ix_yawn_core_groupagentmessage_bot_id")
        batch_op.alter_column(
            "normalized_text",
            existing_type=sa.TEXT(),
            server_default=sa.text("('')"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "role",
            existing_type=sa.VARCHAR(length=16),
            server_default=sa.text("'member'"),
            existing_nullable=False,
        )

    with op.batch_alter_table("yawn_core_groupagentconfig", schema=None) as batch_op:
        batch_op.add_column(sa.Column("expires_at", sa.DATETIME(), nullable=True))
        batch_op.alter_column(
            "tool_allowlist",
            existing_type=sqlite.JSON(),
            server_default=sa.text("'[]'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "admin_tool_daily_limit",
            existing_type=sa.INTEGER(),
            server_default=sa.text("'30'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "admin_tool_count",
            existing_type=sa.INTEGER(),
            server_default=sa.text("'0'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "context_epoch",
            existing_type=sa.INTEGER(),
            server_default=sa.text("'0'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "recent_response_fingerprints",
            existing_type=sqlite.JSON(),
            server_default=sa.text("'[]'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "emotion_state",
            existing_type=sqlite.JSON(),
            server_default=sa.text("'{}'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "proactive_count",
            existing_type=sa.INTEGER(),
            server_default=sa.text("'0'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "media_cache_enabled",
            existing_type=sa.BOOLEAN(),
            server_default=sa.text("0"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "cross_group_visibility",
            existing_type=sa.VARCHAR(length=24),
            server_default=sa.text("'public_summary'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "raw_retention_days",
            existing_type=sa.INTEGER(),
            server_default=sa.text("'7'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "daily_limit",
            existing_type=sa.INTEGER(),
            server_default=sa.text("'12'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "cooldown_minutes",
            existing_type=sa.INTEGER(),
            server_default=sa.text("'20'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "idle_threshold_minutes",
            existing_type=sa.INTEGER(),
            server_default=sa.text("'30'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "proactive_probability",
            existing_type=sa.FLOAT(),
            server_default=sa.text("'0.15'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "trigger_mode",
            existing_type=sa.VARCHAR(length=24),
            server_default=sa.text("'mention_or_proactive'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "persona_version",
            existing_type=sa.INTEGER(),
            server_default=sa.text("'1'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "persona_enabled",
            existing_type=sa.BOOLEAN(),
            server_default=sa.text("1"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "persona_override",
            existing_type=sqlite.JSON(),
            server_default=sa.text("'{}'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "persona",
            existing_type=sa.TEXT(),
            server_default=sa.text("'友好、自然、简洁的群友'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "enabled",
            existing_type=sa.BOOLEAN(),
            server_default=sa.text("1"),
            existing_nullable=False,
        )

    with op.batch_alter_table("yawn_core_agentrelation", schema=None) as batch_op:
        batch_op.drop_index("ix_yawn_core_agentrelation_subject_user_id")
        batch_op.drop_index("ix_yawn_core_agentrelation_object_user_id")
        batch_op.drop_index("ix_yawn_core_agentrelation_group_id")
        batch_op.alter_column(
            "evidence_count",
            existing_type=sa.INTEGER(),
            server_default=sa.text("'1'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "confidence",
            existing_type=sa.FLOAT(),
            server_default=sa.text("'0.5'"),
            existing_nullable=False,
        )

    with op.batch_alter_table("yawn_core_agentprivacy", schema=None) as batch_op:
        batch_op.alter_column(
            "opted_out",
            existing_type=sa.BOOLEAN(),
            server_default=sa.text("0"),
            existing_nullable=False,
        )

    with op.batch_alter_table("yawn_core_agentmemory", schema=None) as batch_op:
        # 若存量数据存在仅 memory_type 不同的行，此步会 IntegrityError。
        batch_op.drop_constraint("uq_agent_memory_key", type_="unique")
        batch_op.create_unique_constraint(
            "uq_agent_memory_key",
            ["scope", "group_id", "subject_user_id", "memory_key"],
        )
        batch_op.drop_index("ix_yawn_core_agentmemory_subject_user_id")
        batch_op.drop_index("ix_yawn_core_agentmemory_scope")
        batch_op.drop_index("ix_yawn_core_agentmemory_memory_type")
        batch_op.drop_index("ix_yawn_core_agentmemory_group_id")
        batch_op.alter_column(
            "visibility",
            existing_type=sa.VARCHAR(length=24),
            server_default=sa.text("'group'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "confidence",
            existing_type=sa.FLOAT(),
            server_default=sa.text("'0.5'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "salience",
            existing_type=sa.FLOAT(),
            server_default=sa.text("'0.5'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "memory_type",
            existing_type=sa.VARCHAR(length=24),
            server_default=sa.text("'summary'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "scope",
            existing_type=sa.VARCHAR(length=16),
            server_default=sa.text("'group'"),
            existing_nullable=False,
        )

    with op.batch_alter_table("yawn_core_agentmediacache", schema=None) as batch_op:
        batch_op.alter_column(
            "size_bytes",
            existing_type=sa.INTEGER(),
            server_default=sa.text("'0'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "status",
            existing_type=sa.VARCHAR(length=24),
            server_default=sa.text("'ready'"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "model_name",
            existing_type=sa.VARCHAR(length=128),
            server_default=sa.text("''"),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "media_type",
            existing_type=sa.VARCHAR(length=24),
            server_default=sa.text("'image'"),
            existing_nullable=False,
        )

    with op.batch_alter_table("yawn_core_agentaudit", schema=None) as batch_op:
        batch_op.drop_index("ix_yawn_core_agentaudit_group_id")
        batch_op.drop_index("ix_yawn_core_agentaudit_created_at")
        batch_op.alter_column(
            "result",
            existing_type=sa.VARCHAR(length=24),
            server_default=sa.text("'success'"),
            existing_nullable=False,
        )
