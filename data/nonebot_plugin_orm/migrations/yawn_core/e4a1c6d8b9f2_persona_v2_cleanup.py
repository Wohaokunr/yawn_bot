"""remove Persona v1 compatibility columns and normalize v2 profiles

Revision ID: e4a1c6d8b9f2
Revises: c9e7a1d4f2b6
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "e4a1c6d8b9f2"
down_revision: str | Sequence[str] | None = "c9e7a1d4f2b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "yawn_core_groupagentconfig"
_SCHEMA_VERSION = 2
_MAX_NOTES = 240

_PRESET_DEFAULTS: dict[str, dict[str, dict[str, int]]] = {
    "natural": {
        "voice": {
            "warmth": 2,
            "humor": 1,
            "directness": 2,
            "verbosity": 1,
            "expressiveness": 1,
        },
        "social": {"sociability": 2, "followup_tendency": 1, "reaction_tendency": 2},
    },
    "gentle_listener": {
        "voice": {
            "warmth": 4,
            "humor": 1,
            "directness": 1,
            "verbosity": 2,
            "expressiveness": 2,
        },
        "social": {"sociability": 2, "followup_tendency": 3, "reaction_tendency": 1},
    },
    "calm_rational": {
        "voice": {
            "warmth": 1,
            "humor": 0,
            "directness": 4,
            "verbosity": 2,
            "expressiveness": 0,
        },
        "social": {"sociability": 1, "followup_tendency": 1, "reaction_tendency": 0},
    },
    "lively_sidekick": {
        "voice": {
            "warmth": 3,
            "humor": 4,
            "directness": 3,
            "verbosity": 1,
            "expressiveness": 4,
        },
        "social": {"sociability": 4, "followup_tendency": 3, "reaction_tendency": 4},
    },
    "quiet_observer": {
        "voice": {
            "warmth": 2,
            "humor": 1,
            "directness": 2,
            "verbosity": 0,
            "expressiveness": 0,
        },
        "social": {"sociability": 0, "followup_tendency": 0, "reaction_tendency": 1},
    },
}
_LEGACY_RUNTIME_SOCIAL = {
    "sociability": 4,
    "followup_tendency": 4,
    "reaction_tendency": 2,
}


def _text(value: object) -> str | None:
    text = " ".join(str(value or "").strip().split())
    return text or None


def _level(value: object, fallback: int) -> int:
    try:
        return max(0, min(4, int(str(value))))
    except (TypeError, ValueError):
        return fallback


def _object_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _legacy_notes(profile: dict[str, Any]) -> list[str]:
    voice = _object_dict(profile.get("voice"))
    principles = _object_dict(profile.get("principles"))
    candidates = (
        ("语气", voice.get("tone")),
        ("表达", voice.get("speech_style")),
        ("基础气质", voice.get("temperament")),
        ("详略", voice.get("response_length")),
        ("角色原则", principles.get("values")),
    )
    return [f"{label}：{text}" for label, value in candidates if (text := _text(value))]


def normalize_profile(raw: object) -> dict[str, Any]:
    """Convert any c9 profile into the final Persona v2-only JSON shape."""

    source = _object_dict(raw)
    identity_raw = _object_dict(source.get("identity"))
    voice_raw = _object_dict(source.get("voice"))
    social_raw = _object_dict(source.get("social"))
    preset_id = str(source.get("preset_id") or "natural").strip().lower()
    if preset_id not in _PRESET_DEFAULTS:
        preset_id = "natural"
    defaults = _PRESET_DEFAULTS[preset_id]

    has_structured = any(
        key in voice_raw
        for key in ("warmth", "humor", "directness", "verbosity", "expressiveness")
    ) or any(
        key in social_raw
        for key in ("sociability", "followup_tendency", "reaction_tendency")
    )
    voice = {
        key: _level(voice_raw.get(key), fallback)
        for key, fallback in defaults["voice"].items()
    }
    social = (
        {
            key: _level(social_raw.get(key), fallback)
            for key, fallback in defaults["social"].items()
        }
        if has_structured
        else dict(_LEGACY_RUNTIME_SOCIAL)
    )
    identity = {
        key: text
        for key in ("name", "description", "group_role")
        if (text := _text(identity_raw.get(key))) is not None
    }
    note_parts: list[str] = []
    existing_notes = _text(source.get("custom_notes"))
    if existing_notes:
        note_parts.append(existing_notes)
    note_parts.extend(_legacy_notes(source))
    custom_notes = "；".join(dict.fromkeys(note_parts))[:_MAX_NOTES]

    result: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "preset_id": preset_id,
        "voice": voice,
        "social": social,
    }
    if identity:
        result["identity"] = identity
    if custom_notes:
        result["custom_notes"] = custom_notes
    # ``legacy_policy_fields`` is intentionally discarded: knowledge/privacy
    # are immutable System Policy rather than Persona data.
    return result


def profile_to_legacy(raw: object) -> dict[str, str]:
    """Best-effort downgrade payload for returning to the c9 schema."""

    profile = _object_dict(raw)
    identity = _object_dict(profile.get("identity"))
    values = {
        "name": identity.get("name"),
        "identity": identity.get("description"),
        "role": identity.get("group_role"),
        "speech_style": profile.get("custom_notes"),
    }
    return {
        key: text for key, value in values.items() if (text := _text(value)) is not None
    }


def upgrade(name: str = "") -> None:
    if name:
        return
    table = sa.table(
        _TABLE,
        sa.column("group_id", sa.BigInteger()),
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
            .values(persona_profile=normalize_profile(row["persona_profile"]))
        )

    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column("persona_schema_version")
        batch_op.drop_column("persona_override")
        batch_op.drop_column("persona")


def downgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column(
                "persona",
                sa.Text(),
                nullable=False,
                server_default="友好、自然、简洁的群友",
            )
        )
        batch_op.add_column(
            sa.Column(
                "persona_override", sa.JSON(), nullable=False, server_default="{}"
            )
        )
        batch_op.add_column(
            sa.Column(
                "persona_schema_version",
                sa.Integer(),
                nullable=False,
                server_default="2",
            )
        )

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
            .values(persona_override=profile_to_legacy(row["persona_profile"]))
        )
