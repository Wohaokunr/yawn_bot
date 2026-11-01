# ruff: noqa: TID252, TRY003
"""「私聊 Agent」页的请求体：提示词（全局 / 本人）与每用户人设。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..yawn_agent.persona import PersonaCard  # noqa: TC001
from .route_models import PromptBulkChange, PromptOverrideMode  # noqa: TC001

PrivateScope = Literal["global", "user"]


class _ScopedBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    scope: PrivateScope = "global"
    user_id: int | None = Field(default=None, alias="userId", gt=0)

    @model_validator(mode="after")
    def user_scope_needs_id(self) -> _ScopedBody:
        if self.scope == "user" and self.user_id is None:
            raise ValueError("本人范围需要 userId")
        return self


class PrivatePromptOverrideBody(_ScopedBody):
    """保存一个私聊分段在全局或某位用户身上的覆盖。"""

    mode: PromptOverrideMode
    text: str = Field(default="", max_length=12_000)
    note: str = Field(default="", max_length=200)
    confirm_safety: bool = Field(default=False, alias="confirmSafety")
    expected_revision: int | None = Field(default=None, alias="expectedRevision", ge=0)


class PrivatePromptBulkBody(_ScopedBody):
    changes: list[PromptBulkChange] = Field(default_factory=list, max_length=400)
    confirm_safety: bool = Field(default=False, alias="confirmSafety")
    expected_revision: int | None = Field(default=None, alias="expectedRevision", ge=0)
    replace_scope: bool = Field(default=False, alias="replaceScope")


class PrivatePromptPreviewBody(BaseModel):
    """用这位用户的真实资料拼一次主回合请求（不发送）。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    user_id: int = Field(alias="userId", gt=0)
    message: str = Field(default="", max_length=2000)
    global_drafts: dict[str, dict[str, Any]] = Field(
        default_factory=dict, alias="globalDrafts", max_length=200
    )
    user_drafts: dict[str, dict[str, Any]] = Field(
        default_factory=dict, alias="userDrafts", max_length=200
    )
    persona_card: PersonaCard | None = Field(default=None, alias="personaCard")
    computer: bool = False
    tasks: bool | None = None
    allow_silence: bool | None = Field(default=None, alias="allowSilence")
    include_history: bool = Field(default=True, alias="includeHistory")


class PrivatePromptPromoteBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    section_ids: list[str] | None = Field(
        default=None, alias="sectionIds", max_length=400
    )
    keep_user: bool = Field(default=False, alias="keepUser")
    confirm_safety: bool = Field(default=False, alias="confirmSafety")
    expected_revision: int | None = Field(default=None, alias="expectedRevision", ge=0)


class PrivateCaptureSettingsBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    paused: bool


class PrivatePersonaPolicyBody(BaseModel):
    """私聊人设策略；没给的字段保持不变。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    enabled: bool | None = None
    self_edit: bool | None = Field(default=None, alias="selfEdit")
    self_edit_daily: int | None = Field(
        default=None, alias="selfEditDaily", ge=0, le=100
    )
    self_fields: list[str] | None = Field(
        default=None, alias="selfFields", max_length=40
    )
    allow_presets: bool | None = Field(default=None, alias="allowPresets")
    allow_library: bool | None = Field(default=None, alias="allowLibrary")
    default_template_id: str | None = Field(
        default=None, alias="defaultTemplateId", max_length=20
    )
    expected_revision: int | None = Field(default=None, alias="expectedRevision", ge=0)


class PrivatePersonaCardBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    card: PersonaCard
    expected_revision: int | None = Field(default=None, alias="expectedRevision", ge=0)


class PrivatePersonaTemplateBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    template_id: int = Field(alias="templateId", gt=0)
    expected_revision: int | None = Field(default=None, alias="expectedRevision", ge=0)


class PrivatePersonaPresetBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    preset_id: str = Field(alias="presetId", min_length=1, max_length=32)
    expected_revision: int | None = Field(default=None, alias="expectedRevision", ge=0)


class PrivatePersonaRevisionBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    expected_revision: int | None = Field(default=None, alias="expectedRevision", ge=0)


class PrivatePersonaLockBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    locked: bool


class PrivatePersonaImportBody(BaseModel):
    """导入 ``yawnbot.private-persona`` 导出文件（只取 card）。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    kind: Literal["yawnbot.private-persona"]
    card: PersonaCard
    expected_revision: int | None = Field(default=None, alias="expectedRevision", ge=0)


class PrivatePersonaSaveTemplateBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(min_length=1, max_length=32)
    switch_user: bool = Field(default=False, alias="switchUser")


class PrivatePersonaCopyGroupBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    group_id: int = Field(alias="groupId", gt=0)
    expected_revision: int | None = Field(default=None, alias="expectedRevision", ge=0)


class PrivatePersonaBatchBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    user_ids: list[int] = Field(alias="userIds", min_length=1, max_length=200)
    action: Literal["reset", "lock", "unlock", "clear"] = "reset"


class PrivatePersonaPreviewBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    card: PersonaCard


__all__ = [
    "PrivateCaptureSettingsBody",
    "PrivatePersonaBatchBody",
    "PrivatePersonaCardBody",
    "PrivatePersonaCopyGroupBody",
    "PrivatePersonaImportBody",
    "PrivatePersonaLockBody",
    "PrivatePersonaPolicyBody",
    "PrivatePersonaPresetBody",
    "PrivatePersonaPreviewBody",
    "PrivatePersonaRevisionBody",
    "PrivatePersonaSaveTemplateBody",
    "PrivatePersonaTemplateBody",
    "PrivatePromptBulkBody",
    "PrivatePromptOverrideBody",
    "PrivatePromptPreviewBody",
    "PrivatePromptPromoteBody",
]
