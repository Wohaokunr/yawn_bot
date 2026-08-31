from __future__ import annotations

import importlib
from typing import Any


def _migration() -> Any:
    return importlib.import_module(
        "data.nonebot_plugin_orm.migrations.yawn_core."
        "c9e7a1d4f2b6_agent_persona_v2"
    )


def test_persona_v2_migration_preserves_structured_and_policy_legacy_fields() -> None:
    migration = _migration()
    profile = migration._legacy_to_profile(
        {
            "tone": "温和",
            "privacy_boundary": "旧版隐私文案",
        },
        "这个单文本不应覆盖已有结构化配置",
    )

    assert profile["schema_version"] == 2  # noqa: PLR2004
    assert profile["voice"]["tone"] == "温和"
    assert "speech_style" not in profile["voice"]
    assert profile["legacy_policy_fields"] == {
        "privacy_boundary": "旧版隐私文案"
    }
    assert migration._profile_to_legacy(profile) == {
        "tone": "温和",
        "privacy_boundary": "旧版隐私文案",
    }


def test_persona_v2_migration_preserves_earliest_single_text_persona() -> None:
    migration = _migration()
    profile = migration._legacy_to_profile({}, "冷淡、少说话、偶尔吐槽")
    assert profile["voice"]["speech_style"] == "冷淡、少说话、偶尔吐槽"

    default_profile = migration._legacy_to_profile({}, "友好、自然、简洁的群友")
    assert "voice" not in default_profile
