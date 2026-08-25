# ruff: noqa: TID252
"""可配置的人设默认值、群级覆盖和稳定序列化。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from nonebot import get_plugin_config
from pydantic import BaseModel, Field

from .log import dbg

if TYPE_CHECKING:
    from ..data_models.group_agent_config import GroupAgentConfig

PERSONA_FIELDS = (
    "name",
    "identity",
    "role",
    "tone",
    "speech_style",
    "values",
    "knowledge_boundary",
    "emotion_baseline",
    "response_length",
    "privacy_boundary",
)
PERSONA_ALIASES = {"style": "speech_style", "length": "response_length"}
MAX_FIELD_LENGTH = 240
_LEGACY_DEFAULT_PERSONAS = frozenset({"友好、自然、简洁的群友"})


class AgentPersonaDefaults(BaseModel):
    """全局默认人设；字段可由环境变量 ``AGENT_PERSONA_*`` 覆盖。"""

    agent_persona_name: str = "Yawn"
    agent_persona_identity: str = "熟悉群聊节奏、自然简洁的普通群友"
    agent_persona_role: str = "普通群友"
    agent_persona_tone: str = "口语化、克制，不刻意热情或装熟"
    agent_persona_speech_style: str = "短句为主，不复述上文，不固定用反问续聊"
    agent_persona_values: str = "尊重事实、尊重边界、先倾听再回答"
    agent_persona_knowledge_boundary: str = "不知道就明确说不知道，不猜测成员隐私"
    agent_persona_emotion_baseline: str = "平静、友善，随对话轻微变化"
    agent_persona_response_length: str = "通常 1-2 句，明确的复杂问题再展开"
    agent_persona_privacy_boundary: str = (
        "不公开私聊内容、隐私记忆、权限信息和工具内部结果"
    )
    agent_persona_version: int = Field(default=1, ge=1)


defaults = get_plugin_config(AgentPersonaDefaults)


def _default_dict() -> dict[str, str]:
    return {
        key: str(getattr(defaults, f"agent_persona_{key}")) for key in PERSONA_FIELDS
    }


def _clean_value(value: object) -> str:
    return " ".join(str(value or "").strip().split())[:MAX_FIELD_LENGTH]


def resolve_persona(config: GroupAgentConfig | None) -> dict[str, str]:
    """合并全局默认、旧版 persona 字段和群级 JSON 覆盖。"""

    result = _default_dict()
    if config is None:
        return result
    legacy = _clean_value(config.persona)
    # 与配置的全局默认值比较，避免环境变量覆盖默认人设后误判旧数据。
    default_identity = _clean_value(defaults.agent_persona_identity)
    if (
        legacy
        and legacy != default_identity
        and legacy not in _LEGACY_DEFAULT_PERSONAS
        and not config.persona_override
    ):
        result["speech_style"] = legacy
    if config.persona_enabled:
        override = (
            config.persona_override if isinstance(config.persona_override, dict) else {}
        )
        for raw_key, value in override.items():
            key = PERSONA_ALIASES.get(str(raw_key), str(raw_key))
            if key in PERSONA_FIELDS:
                cleaned = _clean_value(value)
                if cleaned:
                    result[key] = cleaned
    dbg(
        f"人设解析完成: group={getattr(config, 'group_id', None)} "
        f"persona_enabled={config.persona_enabled} "
        f"persona_version={config.persona_version} persona={result}"
    )
    return result


def parse_persona_assignments(parts: list[str]) -> dict[str, str]:
    """解析 ``key=value``，拒绝未知字段和过长值。"""

    parsed: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            raise ValueError(f"人设参数需要 key=value：{part}")  # noqa: TRY003
        raw_key, raw_value = part.split("=", 1)
        key = PERSONA_ALIASES.get(raw_key.strip().lower(), raw_key.strip().lower())
        if key not in PERSONA_FIELDS:
            raise ValueError(f"不支持的人设字段：{raw_key}")
        value = _clean_value(raw_value)
        if not value:
            raise ValueError(f"人设字段不能为空：{raw_key}")
        parsed[key] = value
    return parsed


def canonical_persona(persona: dict[str, str]) -> str:
    """固定字段顺序，供提示词前缀稳定化使用。"""

    ordered = {key: _clean_value(persona.get(key, "")) for key in PERSONA_FIELDS}
    return json.dumps(
        ordered,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
    )


__all__ = [
    "PERSONA_FIELDS",
    "AgentPersonaDefaults",
    "canonical_persona",
    "defaults",
    "parse_persona_assignments",
    "resolve_persona",
]
