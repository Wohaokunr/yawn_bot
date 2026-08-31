# ruff: noqa: TID252,TRY003
"""WebUI API request models shared by route modules."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..yawn_agent.persona import PersonaEditorProfileV2  # noqa: TC001


class LoginBody(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


class GuestAccessPatch(BaseModel):
    enabled: bool


class GuestGroupAccessPatch(BaseModel):
    allowed: bool


class FeatureOverrideBody(BaseModel):
    override: bool | None


class EnvironmentChange(BaseModel):
    key: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=128)
    value: str | None = Field(default=None, max_length=16384)


class EnvironmentProviderPatch(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    base_url: str = Field(min_length=1, max_length=2048, alias="baseUrl")
    api_key: str | None = Field(
        default=None, min_length=1, max_length=4096, alias="apiKey"
    )


class EnvironmentPatch(BaseModel):
    version: str = Field(min_length=64, max_length=64)
    changes: list[EnvironmentChange] = Field(default_factory=list, max_length=256)
    providers: list[EnvironmentProviderPatch] | None = Field(
        default=None, min_length=1, max_length=17
    )

    @model_validator(mode="after")
    def require_changes(self) -> "EnvironmentPatch":
        if not self.changes and self.providers is None:
            raise ValueError("至少提交一个配置变更")
        return self


class LLMConnectionTestBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provider_id: str = Field(
        pattern=r"^[a-z][a-z0-9_-]{0,31}$", alias="providerId"
    )
    base_url: str | None = Field(default=None, max_length=2048, alias="baseUrl")
    api_key: str | None = Field(
        default=None, min_length=1, max_length=4096, alias="apiKey"
    )
    model: str = Field(min_length=1, max_length=256)



class AgentConfigPatch(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    version: str | None
    enabled: bool | None = None
    # Deprecated compatibility input; new clients use independent flags.
    trigger_mode: (
        Literal[
            "mention_only",
            "mention_or_reply",
            "explicit_wakeup",
            "mention_or_proactive",
        ]
        | None
    ) = Field(default=None, alias="triggerMode")
    reply_trigger_enabled: bool | None = Field(
        default=None, alias="replyTriggerEnabled"
    )
    explicit_wakeup_enabled: bool | None = Field(
        default=None, alias="explicitWakeupEnabled"
    )
    proactive_enabled: bool | None = Field(default=None, alias="proactiveEnabled")
    proactive_probability: float | None = Field(
        default=None, ge=0, le=1, alias="proactiveProbability"
    )
    proactive_active_enabled: bool | None = Field(
        default=None, alias="proactiveActiveEnabled"
    )
    short_conversation_enabled: bool | None = Field(
        default=None, alias="shortConversationEnabled"
    )
    proactive_active_probability: float | None = Field(
        default=None, ge=0, le=1, alias="proactiveActiveProbability"
    )
    proactive_active_window_minutes: int | None = Field(
        default=None, ge=1, le=1440, alias="proactiveActiveWindowMinutes"
    )
    idle_threshold_minutes: int | None = Field(
        default=None, ge=1, le=10080, alias="idleThresholdMinutes"
    )
    cooldown_minutes: int | None = Field(
        default=None, ge=0, le=10080, alias="cooldownMinutes"
    )
    daily_limit: int | None = Field(default=None, ge=0, le=1000, alias="dailyLimit")
    raw_retention_days: int | None = Field(
        default=None, ge=1, le=365, alias="rawRetentionDays"
    )
    cross_group_visibility: Literal["isolated", "public_summary"] | None = Field(
        default=None, alias="crossGroupVisibility"
    )
    media_cache_enabled: bool | None = Field(default=None, alias="mediaCacheEnabled")
    admin_tool_daily_limit: int | None = Field(
        default=None, ge=1, le=1000, alias="adminToolDailyLimit"
    )
    tool_allowlist: list[
        Literal["mute_member", "create_group_announcement", "send_file"]
    ] | None = Field(default=None, alias="toolAllowlist")

    @field_validator("tool_allowlist")
    @classmethod
    def unique_tools(cls, value: list[str] | None) -> list[str] | None:
        return list(dict.fromkeys(value)) if value is not None else None


class PersonaPatch(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    version: str | None
    enabled: bool
    profile: PersonaEditorProfileV2


class AgentDebugRunBody(BaseModel):
    """无副作用的 Agent 提示词回放或模拟试跑。"""

    model_config = ConfigDict(populate_by_name=True)

    mode: Literal["dialogue", "active", "warmup", "followup"] = "dialogue"
    message_id: int | None = Field(default=None, alias="messageId")
    text: str | None = Field(default=None, max_length=4000)
    actor_user_id: Annotated[int, Field(gt=0)] | None = Field(
        default=None, alias="actorUserId"
    )
    run_model: bool = Field(default=False, alias="runModel")
    persona_draft: PersonaEditorProfileV2 | None = Field(
        default=None, alias="personaDraft"
    )

    @model_validator(mode="after")
    def require_one_source(self) -> "AgentDebugRunBody":
        has_history = self.message_id is not None
        has_simulation = (
            bool((self.text or "").strip()) or self.actor_user_id is not None
        )
        if has_history == has_simulation:
            raise ValueError("请选择一条历史消息，或同时填写模拟消息和发言人")
        if has_simulation and (
            not (self.text or "").strip() or self.actor_user_id is None
        ):
            raise ValueError("模拟消息必须同时填写 text 和 actorUserId")
        if self.text is not None:
            self.text = self.text.strip()
        return self


class MemoryCreateBody(BaseModel):
    """手动新增记忆；manual/core 是运维置顶事实，无整理任务回写。"""

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["summary", "profile", "manual", "core"]
    key: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=2000)
    subject_user_id: Annotated[int, Field(gt=0)] | None = Field(
        default=None, alias="subjectUserId"
    )
    related_user_ids: list[Annotated[int, Field(gt=0)]] = Field(
        default_factory=list, max_length=100, alias="relatedUserIds"
    )
    salience: float = Field(default=0.7, ge=0, le=1)
    confidence: float = Field(default=0.9, ge=0, le=1)
    expires_in_days: int | None = Field(
        default=None, ge=1, le=3650, alias="expiresInDays"
    )


class MemoryPatchBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    version: str | None
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    salience: float | None = Field(default=None, ge=0, le=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    expires_in_days: int | None = Field(
        default=None, ge=1, le=3650, alias="expiresInDays"
    )


class PrivacyPatchBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    opted_out: bool = Field(alias="optedOut")


class RelationCreateBody(BaseModel):
    """手动新增关系边；manual 来源的边不会被整理任务与重建覆盖。"""

    model_config = ConfigDict(populate_by_name=True)

    subject_user_id: Annotated[int, Field(gt=0)] = Field(alias="subjectUserId")
    object_user_id: Annotated[int, Field(gt=0)] = Field(alias="objectUserId")
    type: str = Field(min_length=1, max_length=32)
    note: str = Field(default="", max_length=200)
    confidence: float = Field(default=0.9, ge=0, le=1)


class RelationPatchBody(BaseModel):
    """只允许改备注与置信度；类型/两端属于边身份，改动请删除后重建。"""

    model_config = ConfigDict(populate_by_name=True)

    note: str | None = Field(default=None, max_length=200)
    confidence: float | None = Field(default=None, ge=0, le=1)
