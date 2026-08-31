from __future__ import annotations

from pathlib import Path
from typing import Any


def _migration() -> Any:
    import importlib.util

    path = (
        Path(__file__).resolve().parents[1]
        / "src/plugins/yawn_core/migrations/e4a1c6d8b9f2_persona_v2_cleanup.py"
    )
    spec = importlib.util.spec_from_file_location("persona_v2_cleanup_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cleanup_migration_removes_policy_compat_and_preserves_legacy_text() -> None:
    migration = _migration()
    profile = migration.normalize_profile(
        {
            "schema_version": 2,
            "identity": {
                "name": "旧Yawn",
                "description": "旧身份",
                "group_role": "旧群友",
            },
            "voice": {
                "tone": "冷静",
                "speech_style": "短句",
                "temperament": "平静克制",
                "response_length": "一两句",
            },
            "principles": {"values": "尊重事实"},
            "legacy_policy_fields": {
                "knowledge_boundary": "旧知识边界",
                "privacy_boundary": "旧隐私边界",
            },
        }
    )

    assert profile["schema_version"] == 2  # noqa: PLR2004
    assert profile["identity"]["name"] == "旧Yawn"
    assert profile["preset_id"] == "natural"
    assert profile["social"] == {
        "sociability": 4,
        "followup_tendency": 4,
        "reaction_tendency": 2,
    }
    notes = profile["custom_notes"]
    for text in ("冷静", "短句", "平静克制", "一两句", "尊重事实"):
        assert text in notes
    assert "legacy_policy_fields" not in profile
    assert "knowledge_boundary" not in repr(profile)
    assert "privacy_boundary" not in repr(profile)
    assert "旧知识边界" not in repr(profile)
    assert "旧隐私边界" not in repr(profile)


def test_cleanup_migration_preserves_structured_p4_social_traits() -> None:
    migration = _migration()
    profile = migration.normalize_profile(
        {
            "schema_version": 2,
            "preset_id": "quiet_observer",
            "voice": {
                "warmth": 2,
                "humor": 1,
                "directness": 2,
                "verbosity": 0,
                "expressiveness": 0,
            },
            "social": {
                "sociability": 0,
                "followup_tendency": 0,
                "reaction_tendency": 1,
            },
            "custom_notes": "保留这段",
        }
    )

    assert profile["preset_id"] == "quiet_observer"
    assert profile["voice"]["verbosity"] == 0
    assert profile["social"] == {
        "sociability": 0,
        "followup_tendency": 0,
        "reaction_tendency": 1,
    }
    assert profile["custom_notes"] == "保留这段"


def test_cleanup_migration_unknown_preset_falls_back_to_natural() -> None:
    migration = _migration()
    profile = migration.normalize_profile(
        {
            "schema_version": 2,
            "preset_id": "removed-preset",
            "voice": {"warmth": 4},
            "social": {"sociability": 3},
        }
    )
    assert profile["preset_id"] == "natural"
    assert profile["voice"]["warmth"] == 4  # noqa: PLR2004
    assert profile["voice"]["humor"] == 1
    assert profile["social"]["sociability"] == 3  # noqa: PLR2004


def test_cleanup_migration_downgrade_keeps_identity_and_notes_best_effort() -> None:
    migration = _migration()
    legacy = migration.profile_to_legacy(
        {
            "schema_version": 2,
            "identity": {
                "name": "Yawn",
                "description": "群友",
                "group_role": "捧哏",
            },
            "custom_notes": "偶尔接梗",
        }
    )
    assert legacy == {
        "name": "Yawn",
        "identity": "群友",
        "role": "捧哏",
        "speech_style": "偶尔接梗",
    }
