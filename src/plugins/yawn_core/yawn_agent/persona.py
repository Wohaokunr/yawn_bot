# ruff: noqa: TID252
"""Persona v2: structured character profile, runtime behavior and prompt compiler.

P6 intentionally removes the v1 database/command compatibility layer. Historical
rows are normalized by the dedicated P6 migration before these runtime helpers
read them. System policy is not part of Persona.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..data_models.group_agent_config import GroupAgentConfig

from nonebot import get_plugin_config
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .log import dbg

PERSONA_SCHEMA_VERSION = 2
MAX_FIELD_LENGTH = 240
PERSONA_TRAIT_FIELDS = (
    "warmth",
    "humor",
    "directness",
    "verbosity",
    "expressiveness",
    "sociability",
    "followup_tendency",
    "reaction_tendency",
)


class AgentPersonaDefaults(BaseModel):
    """Small global v2 identity surface; detailed style comes from presets."""

    agent_persona_name: str = "Yawn"


class PersonaIdentityV2(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    description: str | None = None
    group_role: str | None = None


class PersonaVoiceV2(BaseModel):
    model_config = ConfigDict(extra="ignore")

    warmth: int | None = Field(default=None, ge=0, le=4)
    humor: int | None = Field(default=None, ge=0, le=4)
    directness: int | None = Field(default=None, ge=0, le=4)
    verbosity: int | None = Field(default=None, ge=0, le=4)
    expressiveness: int | None = Field(default=None, ge=0, le=4)


class PersonaSocialV2(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sociability: int | None = Field(default=None, ge=0, le=4)
    followup_tendency: int | None = Field(default=None, ge=0, le=4)
    reaction_tendency: int | None = Field(default=None, ge=0, le=4)


class PersonaProfileV2(BaseModel):
    """Only the stable v2 schema. Legacy policy/free-text fields are gone."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = Field(default=PERSONA_SCHEMA_VERSION, ge=2)
    preset_id: str | None = None
    identity: PersonaIdentityV2 = Field(default_factory=PersonaIdentityV2)
    voice: PersonaVoiceV2 = Field(default_factory=PersonaVoiceV2)
    social: PersonaSocialV2 = Field(default_factory=PersonaSocialV2)
    custom_notes: str | None = None


@dataclass(frozen=True, slots=True)
class PersonaMutation:
    semantic_changed: bool
    storage_changed: bool


@dataclass(frozen=True, slots=True)
class PersonaBehavior:
    """Persona's bounded influence on group-chat control flow."""

    source: str
    sociability: int
    followup_tendency: int
    reaction_tendency: int
    warmup_probability_scale: float
    active_probability_scale: float
    max_followup_bot_turns: int
    allow_spontaneous_reaction: bool
    reaction_mode: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "sociability": self.sociability,
            "followupTendency": self.followup_tendency,
            "reactionTendency": self.reaction_tendency,
            "warmupProbabilityScale": self.warmup_probability_scale,
            "activeProbabilityScale": self.active_probability_scale,
            "maxFollowupBotTurns": self.max_followup_bot_turns,
            "allowSpontaneousReaction": self.allow_spontaneous_reaction,
            "reactionMode": self.reaction_mode,
        }


@dataclass(frozen=True, slots=True)
class PersonaPreset:
    id: str
    label: str
    description: str
    identity: str
    group_role: str
    warmth: int
    humor: int
    directness: int
    verbosity: int
    expressiveness: int
    sociability: int
    followup_tendency: int
    reaction_tendency: int


class PersonaEditorProfileV2(BaseModel):
    """High-level v2 editor shared by WebUI, QQ commands and Debug drafts."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    preset_id: str = Field(default="natural", alias="presetId", max_length=32)
    name: str = Field(default="Yawn", min_length=1, max_length=64)
    identity: str = Field(default="", max_length=MAX_FIELD_LENGTH)
    group_role: str = Field(default="", alias="groupRole", max_length=MAX_FIELD_LENGTH)
    warmth: int = Field(default=2, ge=0, le=4)
    humor: int = Field(default=1, ge=0, le=4)
    directness: int = Field(default=2, ge=0, le=4)
    verbosity: int = Field(default=1, ge=0, le=4)
    expressiveness: int = Field(default=1, ge=0, le=4)
    sociability: int = Field(default=2, ge=0, le=4)
    followup_tendency: int = Field(default=1, alias="followupTendency", ge=0, le=4)
    reaction_tendency: int = Field(default=2, alias="reactionTendency", ge=0, le=4)
    custom_notes: str = Field(
        default="", alias="customNotes", max_length=MAX_FIELD_LENGTH
    )

    @field_validator("preset_id")
    @classmethod
    def known_preset(cls, value: str) -> str:
        preset_id = value.strip().lower()
        if preset_id not in PERSONA_PRESETS:
            raise ValueError(f"未知人设模板：{value}")
        return preset_id

    @field_validator("name", "identity", "group_role", "custom_notes")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return _clean_value(value)


defaults = get_plugin_config(AgentPersonaDefaults)


PERSONA_PRESETS: dict[str, PersonaPreset] = {
    "natural": PersonaPreset(
        id="natural",
        label="自然群友",
        description="自然、克制、不过度抢话，适合大多数群聊。",
        identity="熟悉群聊节奏、自然简洁的普通群友",
        group_role="普通群友",
        warmth=2,
        humor=1,
        directness=2,
        verbosity=1,
        expressiveness=1,
        sociability=2,
        followup_tendency=1,
        reaction_tendency=2,
    ),
    "gentle_listener": PersonaPreset(
        id="gentle_listener",
        label="温和倾听",
        description="更有耐心、少抢结论，适合陪伴和日常聊天。",
        identity="温和、耐心，愿意先听完再回应的群友",
        group_role="倾听型群友",
        warmth=4,
        humor=1,
        directness=1,
        verbosity=2,
        expressiveness=2,
        sociability=2,
        followup_tendency=3,
        reaction_tendency=1,
    ),
    "calm_rational": PersonaPreset(
        id="calm_rational",
        label="冷静理性",
        description="事实优先、表达直接，适合讨论和答疑。",
        identity="冷静、理性，重视事实和逻辑的群友",
        group_role="理性讨论者",
        warmth=1,
        humor=0,
        directness=4,
        verbosity=2,
        expressiveness=0,
        sociability=1,
        followup_tendency=1,
        reaction_tendency=0,
    ),
    "lively_sidekick": PersonaPreset(
        id="lively_sidekick",
        label="活跃捧哏",
        description="更会接梗和回应气氛，但仍受运行配置限制。",
        identity="活跃、会接梗，但不会喧宾夺主的群友",
        group_role="活跃捧哏",
        warmth=3,
        humor=4,
        directness=3,
        verbosity=1,
        expressiveness=4,
        sociability=4,
        followup_tendency=3,
        reaction_tendency=4,
    ),
    "quiet_observer": PersonaPreset(
        id="quiet_observer",
        label="安静潜水",
        description="存在感更低、少主动延展，适合安静的群。",
        identity="安静、克制，更多观察而不是抢着发言的群友",
        group_role="潜水观察者",
        warmth=2,
        humor=1,
        directness=2,
        verbosity=0,
        expressiveness=0,
        sociability=0,
        followup_tendency=0,
        reaction_tendency=1,
    ),
}

_TRAIT_TEXT: dict[str, tuple[str, ...]] = {
    "warmth": ("偏冷淡", "较克制", "自然", "温和", "很温暖"),
    "humor": ("不主动玩梗", "偶尔轻幽默", "适度幽默", "比较会接梗", "很会接梗"),
    "directness": ("很委婉", "偏委婉", "直接度适中", "比较直接", "非常直接明确"),
    "verbosity": ("极简短", "简洁", "适中", "较详细", "很详细"),
    "expressiveness": (
        "情绪表达很淡",
        "表达克制",
        "自然表达",
        "较有表现力",
        "表现力很强",
    ),
    "sociability": ("很少参与", "偏安静", "社交平衡", "比较主动", "很活跃"),
    "followup_tendency": (
        "不主动续聊",
        "少续聊",
        "适度续聊",
        "较愿意延展",
        "很愿意延展",
    ),
    "reaction_tendency": (
        "几乎不接反应",
        "少量反应",
        "自然反应",
        "较常接反应",
        "很爱接反应",
    ),
}


def _clean_value(value: object) -> str:
    return " ".join(str(value or "").strip().split())[:MAX_FIELD_LENGTH]


def _profile_payload(profile: PersonaProfileV2) -> dict[str, Any]:
    payload = profile.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    payload["schema_version"] = PERSONA_SCHEMA_VERSION
    return payload


def _parse_profile(raw: object) -> PersonaProfileV2 | None:
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        profile = PersonaProfileV2.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        dbg(f"Persona v2 解析失败，使用全局默认：{exc}")
        return None
    if profile.schema_version != PERSONA_SCHEMA_VERSION:
        return None
    return profile


def _profile_for_config(config: GroupAgentConfig | None) -> PersonaProfileV2:
    if config is None:
        return PersonaProfileV2()
    parsed = _parse_profile(getattr(config, "persona_profile", None))
    return parsed if parsed is not None else PersonaProfileV2()


def _trait_text(field: str, value: int) -> str:
    return _TRAIT_TEXT[field][max(0, min(4, int(value)))]


def _default_editor() -> PersonaEditorProfileV2:
    preset = PERSONA_PRESETS["natural"]
    return PersonaEditorProfileV2(
        presetId=preset.id,
        name=str(defaults.agent_persona_name),
        identity=preset.identity,
        groupRole=preset.group_role,
        warmth=preset.warmth,
        humor=preset.humor,
        directness=preset.directness,
        verbosity=preset.verbosity,
        expressiveness=preset.expressiveness,
        sociability=preset.sociability,
        followupTendency=preset.followup_tendency,
        reactionTendency=preset.reaction_tendency,
    )


def persona_preset_payloads() -> list[dict[str, Any]]:
    return [
        {
            "id": preset.id,
            "label": preset.label,
            "description": preset.description,
            "identity": preset.identity,
            "groupRole": preset.group_role,
            "warmth": preset.warmth,
            "humor": preset.humor,
            "directness": preset.directness,
            "verbosity": preset.verbosity,
            "expressiveness": preset.expressiveness,
            "sociability": preset.sociability,
            "followupTendency": preset.followup_tendency,
            "reactionTendency": preset.reaction_tendency,
        }
        for preset in PERSONA_PRESETS.values()
    ]


def persona_editor_profile(config: GroupAgentConfig | None) -> PersonaEditorProfileV2:
    """Return the saved group draft, even when the group currently inherits global."""

    profile = _profile_for_config(config)
    preset = PERSONA_PRESETS.get(
        profile.preset_id or "natural", PERSONA_PRESETS["natural"]
    )
    return PersonaEditorProfileV2(
        presetId=preset.id,
        name=profile.identity.name or str(defaults.agent_persona_name),
        identity=profile.identity.description or preset.identity,
        groupRole=profile.identity.group_role or preset.group_role,
        warmth=(
            profile.voice.warmth
            if profile.voice.warmth is not None
            else preset.warmth
        ),
        humor=profile.voice.humor if profile.voice.humor is not None else preset.humor,
        directness=(
            profile.voice.directness
            if profile.voice.directness is not None
            else preset.directness
        ),
        verbosity=(
            profile.voice.verbosity
            if profile.voice.verbosity is not None
            else preset.verbosity
        ),
        expressiveness=(
            profile.voice.expressiveness
            if profile.voice.expressiveness is not None
            else preset.expressiveness
        ),
        sociability=(
            profile.social.sociability
            if profile.social.sociability is not None
            else preset.sociability
        ),
        followupTendency=(
            profile.social.followup_tendency
            if profile.social.followup_tendency is not None
            else preset.followup_tendency
        ),
        reactionTendency=(
            profile.social.reaction_tendency
            if profile.social.reaction_tendency is not None
            else preset.reaction_tendency
        ),
        customNotes=profile.custom_notes or "",
    )


def _effective_editor(config: GroupAgentConfig | None) -> PersonaEditorProfileV2:
    if config is None or not bool(getattr(config, "persona_enabled", False)):
        return _default_editor()
    return persona_editor_profile(config)


def _resolved_from_editor(draft: PersonaEditorProfileV2) -> dict[str, str]:
    style = "；".join(
        _trait_text(key, int(getattr(draft, key)))
        for key in ("warmth", "humor", "directness", "verbosity", "expressiveness")
    )
    social = "；".join(
        _trait_text(key, int(getattr(draft, key)))
        for key in ("sociability", "followup_tendency", "reaction_tendency")
    )
    result = {
        "profile_v2": "structured",
        "name": draft.name,
        "identity": draft.identity,
        "role": draft.group_role,
        "style_traits": style,
        "social_style": social,
    }
    notes = _clean_value(draft.custom_notes)
    if notes:
        result["custom_notes"] = notes
    return result


_SOCIAL_PROBABILITY_SCALE = (0.15, 0.45, 0.75, 0.9, 1.0)
_FOLLOWUP_MAX_TURNS = (1, 2, 3, 4, 4)
_REACTION_MODES = ("off", "restrained", "normal", "expressive", "high")
_PERSONA_NEUTRAL_LEVEL = 2
_PERSONA_REACTION_AUTO_MIN = 2


def _behavior_from_editor(
    draft: PersonaEditorProfileV2, *, source: str
) -> PersonaBehavior:
    sociability = max(0, min(4, int(draft.sociability)))
    followup = max(0, min(4, int(draft.followup_tendency)))
    reaction = max(0, min(4, int(draft.reaction_tendency)))
    scale = _SOCIAL_PROBABILITY_SCALE[sociability]
    return PersonaBehavior(
        source=source,
        sociability=sociability,
        followup_tendency=followup,
        reaction_tendency=reaction,
        warmup_probability_scale=scale,
        active_probability_scale=scale,
        max_followup_bot_turns=_FOLLOWUP_MAX_TURNS[followup],
        allow_spontaneous_reaction=reaction >= _PERSONA_REACTION_AUTO_MIN,
        reaction_mode=_REACTION_MODES[reaction],
    )


def persona_behavior(config: GroupAgentConfig | None) -> PersonaBehavior:
    source = (
        "global"
        if config is None or not bool(getattr(config, "persona_enabled", False))
        else "persona_v2"
    )
    return _behavior_from_editor(_effective_editor(config), source=source)


def persona_behavior_draft(
    config: GroupAgentConfig | None, draft: PersonaEditorProfileV2
) -> PersonaBehavior:
    del config
    return _behavior_from_editor(draft, source="draft")


def persona_behavior_instruction(
    behavior: PersonaBehavior, *, scene: str
) -> str:
    social = (
        "尽量少抢话"
        if behavior.sociability <= 1
        else "自然参与"
        if behavior.sociability == _PERSONA_NEUTRAL_LEVEL
        else "在有明确切入点时更愿意参与"
    )
    followup = (
        "不要主动续聊"
        if behavior.followup_tendency == 0
        else "续聊要克制"
        if behavior.followup_tendency <= _PERSONA_NEUTRAL_LEVEL
        else "有新信息或明确回应时可以继续承接"
    )
    reaction = {
        "off": "不要主动使用 reaction",
        "restrained": "reaction 极少使用",
        "normal": "reaction 只在自然贴合时使用",
        "expressive": "可以更积极地用 reaction 接住气氛",
        "high": "很适合时可优先用 reaction 接梗，但不要刷屏",
    }[behavior.reaction_mode]
    prefix = "本轮是续聊。" if scene == "followup" else "本轮是主动参与。"
    return f"{prefix} Persona 行为：{social}；{followup}；{reaction}。"


def persona_summary(config: GroupAgentConfig | None) -> str:
    return persona_editor_summary(_effective_editor(config))


def persona_trait_label(field: str, value: int) -> str:
    if field not in _TRAIT_TEXT:
        raise ValueError(f"未知人设特征：{field}")
    return _trait_text(field, value)


def persona_editor_summary(draft: PersonaEditorProfileV2) -> str:
    preset = PERSONA_PRESETS[draft.preset_id]
    return (
        f"{draft.name} · {preset.label} · {_trait_text('warmth', draft.warmth)} · "
        f"{_trait_text('humor', draft.humor)} · "
        f"{_trait_text('verbosity', draft.verbosity)} · "
        f"{_trait_text('sociability', draft.sociability)}"
    )


def persona_editor_apply_preset(
    draft: PersonaEditorProfileV2, preset_id: str
) -> PersonaEditorProfileV2:
    preset = PERSONA_PRESETS.get(preset_id.strip().lower())
    if preset is None:
        raise ValueError(f"未知人设模板：{preset_id}")
    return draft.model_copy(
        update={
            "preset_id": preset.id,
            "identity": preset.identity,
            "group_role": preset.group_role,
            "warmth": preset.warmth,
            "humor": preset.humor,
            "directness": preset.directness,
            "verbosity": preset.verbosity,
            "expressiveness": preset.expressiveness,
            "sociability": preset.sociability,
            "followup_tendency": preset.followup_tendency,
            "reaction_tendency": preset.reaction_tendency,
        }
    )


def _profile_from_editor(draft: PersonaEditorProfileV2) -> PersonaProfileV2:
    return PersonaProfileV2(
        preset_id=draft.preset_id,
        identity=PersonaIdentityV2(
            name=draft.name,
            description=draft.identity,
            group_role=draft.group_role,
        ),
        voice=PersonaVoiceV2(
            warmth=draft.warmth,
            humor=draft.humor,
            directness=draft.directness,
            verbosity=draft.verbosity,
            expressiveness=draft.expressiveness,
        ),
        social=PersonaSocialV2(
            sociability=draft.sociability,
            followup_tendency=draft.followup_tendency,
            reaction_tendency=draft.reaction_tendency,
        ),
        custom_notes=draft.custom_notes or None,
    )


def apply_persona_editor_profile(
    config: GroupAgentConfig,
    draft: PersonaEditorProfileV2,
    *,
    enabled: bool | None = None,
) -> PersonaMutation:
    current_enabled = bool(getattr(config, "persona_enabled", False))
    target_enabled = current_enabled if enabled is None else bool(enabled)
    current_editor = persona_editor_profile(config)
    target_profile = _profile_payload(_profile_from_editor(draft))
    current_payload = getattr(config, "persona_profile", None)
    semantic_changed = (
        current_enabled != target_enabled
        or current_editor.model_dump() != draft.model_dump()
    )
    storage_changed = (
        current_payload != target_profile or current_enabled != target_enabled
    )
    if storage_changed:
        config.persona_profile = target_profile
        config.persona_enabled = target_enabled
    return PersonaMutation(
        semantic_changed=semantic_changed,
        storage_changed=storage_changed,
    )


def resolve_persona(config: GroupAgentConfig | None) -> dict[str, str]:
    result = _resolved_from_editor(_effective_editor(config))
    dbg(
        f"Persona v2 resolved: group={getattr(config, 'group_id', None)} "
        f"custom={bool(config and getattr(config, 'persona_enabled', False))} "
        f"version={getattr(config, 'persona_version', 1)}"
    )
    return result


def resolve_persona_draft(
    config: GroupAgentConfig | None, draft: PersonaEditorProfileV2
) -> dict[str, str]:
    del config
    return _resolved_from_editor(draft)


def reset_persona(config: GroupAgentConfig) -> PersonaMutation:
    """Clear the group draft and truly switch the group back to global Persona."""

    target_profile = _profile_payload(PersonaProfileV2())
    current_enabled = bool(getattr(config, "persona_enabled", False))
    storage_changed = (
        getattr(config, "persona_profile", None) != target_profile or current_enabled
    )
    if storage_changed:
        config.persona_profile = target_profile
        config.persona_enabled = False
    return PersonaMutation(
        semantic_changed=storage_changed,
        storage_changed=storage_changed,
    )


def prompt_persona(persona: dict[str, str]) -> str:
    """Compile only v2 character fields; System Policy is never Persona data."""

    fields = (
        "name",
        "identity",
        "role",
        "style_traits",
        "social_style",
        "custom_notes",
    )
    compact = {
        key: _clean_value(persona.get(key, ""))
        for key in fields
        if _clean_value(persona.get(key, ""))
    }
    return json.dumps(
        compact,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
    )


__all__ = [
    "MAX_FIELD_LENGTH",
    "PERSONA_PRESETS",
    "PERSONA_SCHEMA_VERSION",
    "PERSONA_TRAIT_FIELDS",
    "AgentPersonaDefaults",
    "PersonaBehavior",
    "PersonaEditorProfileV2",
    "PersonaMutation",
    "PersonaPreset",
    "PersonaProfileV2",
    "apply_persona_editor_profile",
    "defaults",
    "persona_behavior",
    "persona_behavior_draft",
    "persona_behavior_instruction",
    "persona_editor_apply_preset",
    "persona_editor_profile",
    "persona_editor_summary",
    "persona_preset_payloads",
    "persona_summary",
    "persona_trait_label",
    "prompt_persona",
    "reset_persona",
    "resolve_persona",
    "resolve_persona_draft",
]
