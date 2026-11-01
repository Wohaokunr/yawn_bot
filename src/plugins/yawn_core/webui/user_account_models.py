"""个人账号登录与自助页面的请求体。

密码字段只在内存里过一遍：不进日志、不进审计（审计中间件只记路径和查询参数名）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_QQ_PATTERN = r"^[1-9][0-9]{4,11}$"


class _Body(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class UserLoginBody(_Body):
    qq: str = Field(pattern=_QQ_PATTERN)
    password: str = Field(min_length=1, max_length=128)


class UserSetupBody(_Body):
    qq: str = Field(pattern=_QQ_PATTERN)
    code: str = Field(min_length=4, max_length=16)
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordBody(_Body):
    current_password: str = Field(alias="currentPassword", min_length=1, max_length=128)
    new_password: str = Field(alias="newPassword", min_length=1, max_length=128)


class GroupFeatureBody(_Body):
    # None = 恢复继承（默认开启）
    override: bool | None


class PersonaFieldsBody(_Body):
    # 只放要改的字段（snake_case，和私聊工具 my_persona 的 fields 一致）
    fields: dict[str, object] = Field(min_length=1, max_length=20)


class PersonaPickBody(_Body):
    id: str = Field(min_length=1, max_length=40)


class AccountDisableBody(_Body):
    disabled: bool


__all__ = [
    "AccountDisableBody",
    "ChangePasswordBody",
    "GroupFeatureBody",
    "PersonaFieldsBody",
    "PersonaPickBody",
    "UserLoginBody",
    "UserSetupBody",
]
