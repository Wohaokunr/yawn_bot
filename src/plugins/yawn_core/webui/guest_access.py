# ruff: noqa: TID252
"""Persistent WebUI guest access policy helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from nonebot_plugin_orm import get_session
from sqlalchemy import func, select

from ..data_models.guest_access import GuestAccessConfig, GuestGroupAccess

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_CONFIG_ID = 1
_CREDENTIAL_DOMAIN = b"YawnBot WebUI guest credential\0"


@dataclass(frozen=True, slots=True)
class GuestPolicySnapshot:
    enabled: bool
    credential_configured: bool
    credential_version: int
    authorized_group_count: int
    updated_at: datetime | None


def hash_guest_credential(value: str) -> str:
    return hashlib.sha256(_CREDENTIAL_DOMAIN + value.encode("utf-8")).hexdigest()


def generate_guest_credential() -> str:
    # 32 random bytes ~= 256 bits. Prefix makes the one-time code recognizable.
    return f"guest_{secrets.token_urlsafe(32)}"


def credential_matches(value: str, expected_hash: str | None) -> bool:
    if not expected_hash:
        return False
    supplied_hash = hash_guest_credential(value)
    return hmac.compare_digest(supplied_hash, expected_hash)


def apply_enabled(config: GuestAccessConfig, *, enabled: bool) -> None:
    if config.enabled and not enabled:
        config.credential_version = int(config.credential_version) + 1
    config.enabled = enabled
    config.updated_at = utc_now()


def apply_new_credential(config: GuestAccessConfig, credential: str) -> None:
    config.credential_hash = hash_guest_credential(credential)
    config.credential_version = int(config.credential_version) + 1
    config.updated_at = utc_now()


async def get_config(db: AsyncSession) -> GuestAccessConfig | None:
    return await db.get(GuestAccessConfig, _CONFIG_ID)


async def get_or_create_config(db: AsyncSession) -> GuestAccessConfig:
    row = await get_config(db)
    if row is not None:
        return row
    row = GuestAccessConfig(id=_CONFIG_ID, enabled=False, credential_version=0)
    db.add(row)
    await db.flush()
    return row


async def policy_snapshot(db: AsyncSession) -> GuestPolicySnapshot:
    row = await get_config(db)
    count = int(
        await db.scalar(select(func.count()).select_from(GuestGroupAccess)) or 0
    )
    if row is None:
        return GuestPolicySnapshot(
            enabled=False,
            credential_configured=False,
            credential_version=0,
            authorized_group_count=count,
            updated_at=None,
        )
    return GuestPolicySnapshot(
        enabled=bool(row.enabled),
        credential_configured=bool(row.credential_hash),
        credential_version=int(row.credential_version),
        authorized_group_count=count,
        updated_at=row.updated_at,
    )


async def authenticate_guest_credential(value: str) -> GuestPolicySnapshot | None:
    async with get_session() as db:
        row = await get_config(db)
        if (
            row is None
            or not row.enabled
            or not credential_matches(value, row.credential_hash)
        ):
            return None
        count = int(
            await db.scalar(select(func.count()).select_from(GuestGroupAccess)) or 0
        )
        return GuestPolicySnapshot(
            enabled=True,
            credential_configured=True,
            credential_version=int(row.credential_version),
            authorized_group_count=count,
            updated_at=row.updated_at,
        )


async def guest_session_is_current(credential_version: int | None) -> bool:
    if credential_version is None:
        return False
    async with get_session() as db:
        row = await get_config(db)
        return bool(
            row is not None
            and row.enabled
            and row.credential_hash
            and int(row.credential_version) == credential_version
        )


async def guest_group_is_allowed(group_id: int) -> bool:
    async with get_session() as db:
        row = await get_config(db)
        if row is None or not row.enabled or not row.credential_hash:
            return False
        return await db.get(GuestGroupAccess, group_id) is not None


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
