"""add Agent Persona v2 structured profile

Revision ID: c9e7a1d4f2b6
Revises: b4d6e8f0a2c4
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "c9e7a1d4f2b6"
down_revision: str | Sequence[str] | None = "b4d6e8f0a2c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "yawn_core_groupagentconfig"
_SCHEMA_VERSION = 2
_POLICY_FIELDS = ("knowledge_boundary", "privacy_boundary")
_LEGACY_PERSONA_DEFAULTS = {
    "友好、自然、简洁的群友",
    "熟悉群聊节奏、自然简洁的普通群友",
}


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _legacy_to_profile(raw: object, legacy_persona: object = None) -> dict[str, Any]:
    values = raw if isinstance(raw, dict) else {}
    identity = {
        "name": _text(values.get("name")),
        "description": _text(values.get("identity")),
        "group_role": _text(values.get("role")),
    }
    voice = {
        "tone": _text(values.get("tone")),
        "speech_style": _text(values.get("speech_style") or values.get("style")),
        "temperament": _text(values.get("emotion_baseline")),
        "response_length": _text(values.get("response_length") or values.get("length")),
    }
    if not values:
        legacy_style = _text(legacy_persona)
        if legacy_style and legacy_style not in _LEGACY_PERSONA_DEFAULTS:
            voice["speech_style"] = legacy_style
    principles = {"values": _text(values.get("values"))}
    legacy_policy = {
        key: str(values[key])
        for key in _POLICY_FIELDS
        if _text(values.get(key)) is not None
    }
    profile: dict[str, Any] = {"schema_version": _SCHEMA_VERSION}
    if any(identity.values()):
        profile["identity"] = {key: value for key, value in identity.items() if value}
    if any(voice.values()):
        profile["voice"] = {key: value for key, value in voice.items() if value}
    if any(principles.values()):
        profile["principles"] = {
            key: value for key, value in principles.items() if value
        }
    if legacy_policy:
        profile["legacy_policy_fields"] = legacy_policy
    return profile


def _profile_to_legacy(raw: object) -> dict[str, str]:
    profile = raw if isinstance(raw, dict) else {}
    identity = profile.get("identity") if isinstance(profile.get("identity"), dict) else {}
    voice = profile.get("voice") if isinstance(profile.get("voice"), dict) else {}
    principles = (
        profile.get("principles")
        if isinstance(profile.get("principles"), dict)
        else {}
    )
    legacy_policy = (
        profile.get("legacy_policy_fields")
        if isinstance(profile.get("legacy_policy_fields"), dict)
        else {}
    )
    values = {
        "name": identity.get("name"),
        "identity": identity.get("description"),
        "role": identity.get("group_role"),
        "tone": voice.get("tone"),
        "speech_style": voice.get("speech_style"),
        "emotion_baseline": voice.get("temperament"),
        "response_length": voice.get("response_length"),
        "values": principles.get("values"),
        **legacy_policy,
    }
    return {
        key: text
        for key, value in values.items()
        if (text := _text(value)) is not None
    }


def upgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column(
                "persona_schema_version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.add_column(
            sa.Column(
                "persona_profile",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )

    table = sa.table(
        _TABLE,
        sa.column("group_id", sa.BigInteger()),
        sa.column("persona", sa.Text()),
        sa.column("persona_override", sa.JSON()),
        sa.column("persona_schema_version", sa.Integer()),
        sa.column("persona_profile", sa.JSON()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(table.c.group_id, table.c.persona, table.c.persona_override)
    ).mappings()
    for row in rows:
        connection.execute(
            sa.update(table)
            .where(table.c.group_id == row["group_id"])
            .values(
                persona_schema_version=_SCHEMA_VERSION,
                persona_profile=_legacy_to_profile(
                    row["persona_override"], row["persona"]
                ),
            )
        )

    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.alter_column(
            "persona_schema_version",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "persona_profile",
            existing_type=sa.JSON(),
            existing_nullable=False,
            server_default=None,
        )


def downgrade(name: str = "") -> None:
    if name:
        return
    table = sa.table(
        _TABLE,
        sa.column("group_id", sa.BigInteger()),
        sa.column("persona_override", sa.JSON()),
        sa.column("persona_profile", sa.JSON()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(table.c.group_id, table.c.persona_profile)
    ).mappings()
    for row in rows:
        connection.execute(
            sa.update(table)
            .where(table.c.group_id == row["group_id"])
            .values(persona_override=_profile_to_legacy(row["persona_profile"]))
        )

    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column("persona_profile")
        batch_op.drop_column("persona_schema_version")
