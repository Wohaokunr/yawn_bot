# ruff: noqa: TC003,TRY003
"""Provider-specific media transport for Agent multimodal inputs."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

MediaKind = Literal["image"]


@dataclass(frozen=True, slots=True)
class MediaInput:
    """Provider-neutral media input carried inside the Agent pipeline."""

    kind: MediaKind
    content_hash: str
    mime_type: str
    local_path: Path | None
    data: bytes | None = field(default=None, repr=False, compare=False)
    provider: str = "local"
    provider_scope: str = "local"
    remote_file_id: str | None = None
    remote_created_at: datetime | None = None
    remote_expires_at: datetime | None = None
    source_url: str | None = None
    source: str = "current"
    source_message_id: int | None = None
    asset_id: int | None = None

    def bind_provider(
        self,
        *,
        provider: str,
        provider_scope: str,
        remote_file_id: str | None = None,
        remote_created_at: datetime | None = None,
        remote_expires_at: datetime | None = None,
    ) -> "MediaInput":
        return replace(
            self,
            provider=provider,
            provider_scope=provider_scope,
            remote_file_id=remote_file_id,
            remote_created_at=remote_created_at,
            remote_expires_at=remote_expires_at,
        )


class MediaProvider(Protocol):
    """Provider media lifecycle and request-block adapter."""

    provider_id: str
    provider_scope: str

    async def upload(
        self, media: MediaInput, *, expires_after_seconds: int
    ) -> MediaInput: ...

    async def retrieve(self, remote_file_id: str) -> dict[str, Any] | None: ...

    async def delete(self, remote_file_id: str) -> bool: ...

    def build_content_block(self, media: MediaInput) -> dict[str, Any]: ...


def provider_scope(provider_id: str, api_key: str | None) -> str:
    """Return a non-secret scope that invalidates remote IDs after API-key rotation."""

    if not api_key:
        return f"{provider_id}:anonymous"
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:24]
    return f"{provider_id}:{digest}"


def is_deepseek_files_endpoint(base_url: str) -> bool:
    """Only enable Files API semantics for DeepSeek's official API endpoint."""

    host = (urlparse(base_url).hostname or "").lower()
    return host == "api.deepseek.com" or host.endswith(".api.deepseek.com")


def _data_url(data: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _naive_beijing_from_epoch(value: object) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    utc_value = datetime.fromtimestamp(float(value), tz=timezone.utc)
    return utc_value.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)


class InlineMediaProvider:
    """Safe OpenAI-compatible fallback using materialized local bytes."""

    def __init__(self, provider_id: str, provider_scope_value: str) -> None:
        self.provider_id = provider_id
        self.provider_scope = provider_scope_value

    async def upload(
        self, media: MediaInput, *, expires_after_seconds: int
    ) -> MediaInput:
        del expires_after_seconds
        return media.bind_provider(
            provider=self.provider_id,
            provider_scope=self.provider_scope,
        )

    async def retrieve(self, remote_file_id: str) -> dict[str, Any] | None:
        del remote_file_id
        return None

    async def delete(self, remote_file_id: str) -> bool:
        del remote_file_id
        return False

    def build_content_block(self, media: MediaInput) -> dict[str, Any]:
        if media.data is not None:
            return {
                "type": "image_url",
                "image_url": {"url": _data_url(media.data, media.mime_type)},
            }
        if media.local_path is not None and media.local_path.is_file():
            return {
                "type": "image_url",
                "image_url": {
                    "url": _data_url(media.local_path.read_bytes(), media.mime_type)
                },
            }
        if media.source_url:
            return {"type": "image_url", "image_url": {"url": media.source_url}}
        raise ValueError("濯掍綋鏃㈡病鏈夋湰鍦版枃浠讹紝涔熸病鏈夊彲鐢?URL")


class DeepSeekFileProvider(InlineMediaProvider):
    """DeepSeek Files API transport for reusable image inputs."""

    def __init__(
        self,
        provider_id: str,
        provider_scope_value: str,
        client: Any,
    ) -> None:
        super().__init__(provider_id, provider_scope_value)
        self._client = client

    async def upload(
        self, media: MediaInput, *, expires_after_seconds: int
    ) -> MediaInput:
        if media.remote_file_id and media.provider_scope == self.provider_scope:
            return media
        if media.local_path is None and media.data is None:
            return await super().upload(
                media, expires_after_seconds=expires_after_seconds
            )
        ttl = min(max(int(expires_after_seconds), 3600), 2_592_000)
        suffix = mimetypes.guess_extension(media.mime_type) or ".bin"
        created = await self._client.files.create(
            file=(
                media.local_path
                if media.local_path is not None and media.local_path.is_file()
                else (f"{media.content_hash}{suffix}", media.data, media.mime_type)
            ),
            purpose="user_data",
            expires_after={"anchor": "created_at", "seconds": ttl},
        )
        remote_file_id = str(getattr(created, "id", "") or "").strip()
        if not remote_file_id:
            raise RuntimeError("DeepSeek Files API 鏈繑鍥?file_id")
        return media.bind_provider(
            provider=self.provider_id,
            provider_scope=self.provider_scope,
            remote_file_id=remote_file_id,
            remote_created_at=_naive_beijing_from_epoch(
                getattr(created, "created_at", None)
            ),
            remote_expires_at=_naive_beijing_from_epoch(
                getattr(created, "expires_at", None)
            ),
        )

    async def retrieve(self, remote_file_id: str) -> dict[str, Any] | None:
        item = await self._client.files.retrieve(remote_file_id)
        return {
            "id": str(getattr(item, "id", "") or ""),
            "bytes": getattr(item, "bytes", None),
            "created_at": _naive_beijing_from_epoch(getattr(item, "created_at", None)),
            "expires_at": _naive_beijing_from_epoch(getattr(item, "expires_at", None)),
        }

    async def delete(self, remote_file_id: str) -> bool:
        result = await self._client.files.delete(remote_file_id)
        deleted = getattr(result, "deleted", None)
        return bool(True if deleted is None else deleted)

    def build_content_block(self, media: MediaInput) -> dict[str, Any]:
        if media.remote_file_id and media.provider_scope == self.provider_scope:
            return {"type": "file", "file_id": media.remote_file_id}
        return super().build_content_block(media)

    def build_file_data_block(self, media: MediaInput) -> dict[str, Any]:
        data = media.data
        if data is None and media.local_path is not None and media.local_path.is_file():
            data = media.local_path.read_bytes()
        if data is None:
            raise ValueError("media has no local bytes for file_data fallback")
        suffix = mimetypes.guess_extension(media.mime_type) or ".bin"
        return {
            "type": "file",
            "file_data": _data_url(data, media.mime_type),
            "filename": f"{media.content_hash}{suffix}",
        }


__all__ = [
    "DeepSeekFileProvider",
    "InlineMediaProvider",
    "MediaInput",
    "MediaProvider",
    "is_deepseek_files_endpoint",
    "provider_scope",
]
