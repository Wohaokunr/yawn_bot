# ruff: noqa: C901,PLR0913,PLR0917,TID252,TC001
"""Dedicated image-caption fallback used by the Agent dialogue pipeline."""

from __future__ import annotations

from typing import Any

from ..data_models.group_agent_config import GroupAgentConfig
from ..llm import complete, resolve_llm_request, vision_model_configured
from .log import dbg
from .media import MediaInput, build_media_content_blocks, store_caption
from .message_parser import NormalizedMessage

_VISION_SYSTEM_PROMPT = (
    "你是图片识别器。只描述图片中可见且与用户问题相关的事实，"
    "不猜测身份、隐私或图片外的信息。"
)


async def _caption_single_image(
    group_id: int,
    normalized: NormalizedMessage,
    media: MediaInput,
    session: Any,
    config: GroupAgentConfig,
    diagnostics: list[dict[str, Any]] | None = None,
) -> str | None:
    blocks, _bound = await build_media_content_blocks(
        [media],
        task="agent_image",
        group_id=group_id,
        session=session,
        cache_enabled=bool(config.media_cache_enabled),
        diagnostics=diagnostics,
    )
    if not blocks:
        return None
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _VISION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": normalized.prompt_text()},
                blocks[0],
            ],
        },
    ]
    result = await complete(  # pyright: ignore[reportArgumentType]
        messages,  # pyright: ignore[reportArgumentType]
        task="agent_image",
        max_tokens=500,
        timeout=30,
    )  # pyright: ignore[reportArgumentType]
    return (result or "").strip() or None


def _media_diagnostic(
    media: MediaInput,
    *,
    provider: str,
    model: str,
    **values: Any,
) -> dict[str, Any]:
    return {
        "source": media.source,
        "source_message_id": media.source_message_id,
        "asset_id": media.asset_id,
        "content_hash": media.content_hash[:12],
        "_content_hash": media.content_hash,
        "provider": provider,
        "model": model,
        **values,
    }


async def describe_images(
    group_id: int,
    normalized: NormalizedMessage,
    media_inputs: list[MediaInput],
    session: Any,
    config: GroupAgentConfig,
    cached: list[tuple[str, str]],
    digests: list[str],
    diagnostics: list[dict[str, Any]] | None = None,
) -> str:
    """Describe images one by one and cache each caption under its own digest."""

    has_vision_model = vision_model_configured()
    if not media_inputs:
        dbg(f"群 {group_id} 跳过图片识别: 无可用图片 block")
        return "[图片未识别：没有可用的图片数据]"

    caption_by_digest = dict(cached)
    vision_request = resolve_llm_request("agent_image")
    parts: list[str] = []
    digest_set = set(digests)
    for media in media_inputs:
        digest = media.content_hash if media.content_hash in digest_set else None
        caption = caption_by_digest.get(digest) if digest is not None else None
        if caption:
            parts.append(f"[图片转述（缓存）] {caption}")
            if diagnostics is not None:
                diagnostics.append(
                    _media_diagnostic(
                        media,
                        provider=vision_request.provider,
                        model=vision_request.model,
                        status="caption_ready",
                        caption_cache="hit",
                        input_type="caption",
                        vision_status="cached_caption",
                        delivered_to_model=True,
                    )
                )
            continue

        if not has_vision_model:
            parts.append("[图片未识别：当前未配置可用的识图模型]")
            if diagnostics is not None:
                diagnostics.append(
                    _media_diagnostic(
                        media,
                        provider=vision_request.provider,
                        model=vision_request.model,
                        status="vision_unsupported",
                        input_type="none",
                        vision_status="model_not_configured",
                        delivered_to_model=False,
                        reason="agent_image_route_not_configured",
                    )
                )
            continue

        caption = await _caption_single_image(
            group_id,
            normalized,
            media,
            session,
            config,
            diagnostics=diagnostics,
        )
        if caption is None:
            dbg(f"群 {group_id} 视觉模型返回空结果 digest={digest}")
            parts.append("[图片未识别：视觉模型没有返回结果]")
            if diagnostics is not None:
                diagnostics.append(
                    _media_diagnostic(
                        media,
                        provider=vision_request.provider,
                        model=vision_request.model,
                        status="vision_failed",
                        vision_status="empty_result",
                        reason="agent_image_model_returned_empty",
                    )
                )
            continue

        dbg(f"群 {group_id} 视觉模型识别完成 digest={digest} caption={caption!r}")
        if digest:
            await store_caption(
                session,
                group_id,
                digest,
                caption,
                vision_request.model,
                cache_enabled=bool(config.media_cache_enabled),
            )
        parts.append(f"[图片转述] {caption[:2000]}")
        if diagnostics is not None:
            diagnostics.append(
                _media_diagnostic(
                    media,
                    provider=vision_request.provider,
                    model=vision_request.model,
                    vision_status="caption_ready",
                    delivered_to_model=True,
                )
            )
    return "\n".join(parts)


__all__ = ["describe_images"]
